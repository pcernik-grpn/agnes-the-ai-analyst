"""KaiEngineProvider — run web-chat sessions on the embedded kai-agent turn engine.

A ``SandboxProvider`` (``chat.provider: kai-agent``), next to ``docker``
and ``docker`` — except the "sandbox" it provides is the embedded `kai-agent`
turn engine this instance already hosts for ``app/api/kai.py``. The engine
owns its own agent loop, its own conversation store and its own remote
execution sandbox, so unlike the sibling providers nothing is spawned here:
``spawn()`` returns an in-process handle that speaks the runner's stdio frame
protocol on one side and the engine's HTTP surface on the other.

Why a provider and not a new transport: the frame protocol is the currency of
the whole chat stack — the manager persists, replays, meters and fans out
*frames*, and the web client renders them over the existing WebSocket. A
handle that translates the engine's SSE stream into those frames gets every
existing behavior (history, mid-turn reconnect replay, sinks, auto-title,
message-rate limits) without touching the manager or a line of frontend code.
``app/chat/harness.py`` documents the same decoupling for in-sandbox engines;
this module is the shape it takes for an engine that replaces the sandbox as
well as the loop.

The translation, per turn (``user_msg`` stdin frame → one ``POST /api/chat``):

    engine SSE event (AI-SDK UI stream)      runner frame
    ---------------------------------------  ---------------------------------
    text-delta {delta}                       token {text}
    tool-input-available {toolCallId, ...}   tool_call {tool_use_id, tool, args}
    tool-approval-request {toolCallId}       approval_request {request_id, ...}
    tool-output-available {toolCallId, out}  tool_result {tool_use_id, result}
    tool-output-error {toolCallId, errText}  tool_result {tool_use_id, result}
    error {errorText}                        error {kind: engine_error}
    finish / stream end                      assistant_message + done

``cancel`` maps to ``POST /api/chat/{id}/stop`` and ``approval_decision`` to
``POST /api/chat/{id}/approval`` (the handle advertises
``supportsApprovalRequestedEvent`` so the engine raises approvals as events
instead of parking them on a UI heuristic; with ``chat.approvals_enabled``
off, the handle auto-denies each request — the same instant-deny the native
gate applies). ``ticket_push`` frames are dropped: the engine authenticates
with the session JWT this module mints through
``app.api.kai.mint_engine_session_token`` — the same claims contract
``POST /api/kai/sessions`` serves to external consumers — and its sandbox
egress rides the per-turn tickets the engine itself mints at
``/api/kai/tickets``. The provider declares ``provides_own_credentials`` so
the manager neither mints the native ``main``/``mcp``/``data_apps`` scopes
(credentials no engine session could redeem) nor scope-blind-revokes the
engine's live per-turn tickets on resume/respawn (which would 401 an
in-flight answer for nothing).

Session ids are UUIDs (the engine stores ``body.id`` in a Postgres ``uuid``
column); both session creators — ``ChatManager.create_session`` and the
producer-side ``resolve_or_create_slack_session`` — mint them in that shape
when this provider is configured. A pre-existing ``chat_<hex>`` session
attached under this provider gets a DEAD handle that answers every message
with a clear error frame instead of an opaque engine 400 or a bare WS drop.

Lifecycle mapping: the handle holds no remote resource, so ``pause`` is a
plain teardown (the engine parks its own sandbox and keeps the transcript in
its own Postgres), ``resume`` builds a fresh handle for the same chat id, and
``keepalive``/``destroy`` are no-ops. A handle only resolves ``wait()``
non-zero when its internal loop dies unexpectedly — the manager's
crash-respawn then rebuilds it, and conversation continuity comes from the
engine's own store rather than a restore-context upload (``stage_file`` is
deliberately not implemented).

Known limitations, stated rather than implied (also in docs/cloud-chat.md):
the engine does not surface token usage on its stream, so
``chat.daily_anthropic_spend_usd`` / ``chat.max_session_tokens`` do not meter
engine sessions (message-rate and concurrency caps still apply); agent
personas and memories DO reach an engine turn — ``GET /api/kai/workspace``
packs them into the per-session tarball (``_agent_workspace_members``) and a
scoped agent's tools resolve to a live ``AgentPrincipal`` at
``/api/kai/mcp`` — but the co-drive grant-intersection workspace still does
not (a co-session runs as a conversation without host data access), and the
agent memory WRITE side has no engine channel (the remember endpoint is
unreachable from the engine sandbox, so ``memory_write_mode`` is read-only
there); ``chat.per_tool_call_seconds`` and ``chat.tool_calls_per_turn_budget``
are enforced by the engine's own policies, not these knobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

# The shared queue+EOF StreamReader shim and the str→bytes write coercion —
# the same cross-provider imports docker_provider.py already makes, so all
# three providers' handle streams behave identically for host-side readers.
from app.chat.provider import _coerce_to_bytes, _StreamReaderAdapter

logger = logging.getLogger(__name__)

#: ``sandbox_id`` namespace persisted via ``set_sandbox_ref`` — carries the
#: chat id so ``resume()`` (which may receive an empty env on legacy paths)
#: can rebuild a handle without any provider-side state.
_SANDBOX_ID_PREFIX = "kai-engine:"

_CONNECT_TIMEOUT_SECONDS = 15.0
#: The engine emits an ``:ping`` SSE comment every 30 s specifically so
#: intermediaries keep idle turns alive — four consecutive missed pings means
#: the stream is genuinely stalled, not merely quiet while a tool runs.
_SSE_READ_TIMEOUT_SECONDS = 120.0
#: Re-mint the session JWT when it has less runway than one generous turn.
_TOKEN_REFRESH_MARGIN_SECONDS = 15 * 60
#: Fallback for the approval card's ``timeout_seconds`` label when the spawn
#: env carries no ``AGNES_APPROVAL_TIMEOUT_SECONDS`` (the operator knob the
#: manager threads to every provider); the engine enforces its own window
#: server-side, this value only labels the card.
_APPROVAL_TIMEOUT_FALLBACK_SECONDS = 300


def _default_mint(user_email: str, session_id: str) -> tuple[str, int]:
    """Mint the engine session JWT via the host wiring's own helper.

    Late import: ``app.api.kai`` pulls FastAPI and the repo factories, none of
    which this module needs at import time (tests inject a fake mint).
    """
    from app.api.kai import mint_engine_session_token

    return mint_engine_session_token(user_email, session_id)


class _FrameReader(_StreamReaderAdapter):
    """The shared stdout adapter plus dict-level framing.

    ``feed_frame`` owns the newline framing so no caller can emit a torn
    line; everything else (EOF sentinel, ``readline``, size-honoring
    ``read``) is the same class the docker handles expose, so
    host-side readers see identical stream semantics on all three providers.
    """

    def feed_frame(self, frame: dict) -> None:
        self.feed((json.dumps(frame) + "\n").encode("utf-8"))


class _StdinFrames:
    """asyncio.StreamWriter-like adapter parsing the manager's stdin frames.

    The manager writes one newline-terminated JSON frame per ``write`` +
    ``drain`` pair, but this buffers and splits defensively so a coalesced or
    torn write cannot desync the stream.
    """

    def __init__(self, queue: "asyncio.Queue[dict]") -> None:
        self._queue = queue
        self._buf = bytearray()
        self._closed = False

    def write(self, data) -> None:
        if self._closed:
            raise RuntimeError("stdin closed")
        self._buf.extend(_coerce_to_bytes(data))

    async def drain(self) -> None:
        while True:
            idx = self._buf.find(b"\n")
            if idx == -1:
                return
            line = bytes(self._buf[: idx + 1])
            del self._buf[: idx + 1]
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("kai engine handle: dropping non-JSON stdin line")
                continue
            if isinstance(frame, dict):
                self._queue.put_nowait(frame)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None


class _TurnState:
    """Frame-translation state for one engine turn."""

    def __init__(self) -> None:
        #: Text deltas per part id, in first-seen order. One structure for
        #: open and closed segments alike: ``text-start``/``text-end`` need no
        #: bookkeeping, a stream that dies mid-segment still contributes what
        #: it streamed, and a straggler delta after ``text-end`` appends to
        #: its own part instead of re-opening it out of order.
        self.parts: dict[str, list[str]] = {}
        #: toolCallId → tool name, for approval cards raised after the input.
        self.tool_names: dict[str, str] = {}
        #: toolCallId → args, for the approval card's command preview.
        self.tool_args: dict[str, Any] = {}
        #: Approval cards raised and not yet resolved (by a web decision, an
        #: engine-side outcome, or turn-end retirement — whichever is first).
        self.pending_approvals: set[str] = set()
        #: SSE payloads that failed to parse — counted so a contract drift
        #: does not degrade into a silently empty answer (the same reason
        #: the CLI's SSE consumer keeps a drop counter).
        self.dropped_events = 0

    def text(self) -> str:
        # Blank line between parts — the same block join the runner applies
        # to consolidated TextBlocks.
        return "\n\n".join(p for p in ("".join(deltas).strip() for deltas in self.parts.values()) if p)


class KaiEngineHandle:
    """SandboxHandle adapter driving one chat's turns against the engine.

    ``fail_reason`` builds a DEAD handle: it emits the reason as an error
    frame at boot and again for every ``user_msg``, and never contacts the
    engine — the legible surface for a session whose id the engine cannot
    accept (see ``KaiEngineProvider.spawn``).
    """

    def __init__(
        self,
        *,
        chat_id: str,
        user_email: str,
        base_url: str,
        mint: Callable[[str, str], tuple[str, int]],
        transport: Optional[httpx.AsyncBaseTransport] = None,
        approval_timeout_seconds: int = _APPROVAL_TIMEOUT_FALLBACK_SECONDS,
        approvals_enabled: bool = True,
        fail_reason: Optional[str] = None,
    ) -> None:
        self.pid = 1  # no host process; a stable placeholder for set_sandbox_ref
        self.sandbox_id = f"{_SANDBOX_ID_PREFIX}{chat_id}"
        self._chat_id = chat_id
        self._user_email = user_email
        self._base_url = base_url.rstrip("/")
        self._mint = mint
        self._approval_timeout_seconds = approval_timeout_seconds
        self._approvals_enabled = approvals_enabled
        self._fail_reason = fail_reason
        self._token: Optional[str] = None
        self._token_expires_at = 0
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT_SECONDS,
                read=_SSE_READ_TIMEOUT_SECONDS,
                write=_CONNECT_TIMEOUT_SECONDS,
                pool=_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self._frames: "asyncio.Queue[dict]" = asyncio.Queue()
        self.stdin = _StdinFrames(self._frames)
        self.stdout = _FrameReader()
        self.stderr = _FrameReader()
        self.stderr.feed_eof()
        self._exit: "asyncio.Future[int]" = asyncio.get_running_loop().create_future()
        self._turn_task: Optional[asyncio.Task] = None
        #: The in-flight turn's translation state — so a web approval decision
        #: (which arrives outside the turn task) can retire the card from the
        #: same pending set the turn's own resolution paths use, keeping every
        #: card resolved exactly once.
        self._turn_state: Optional[_TurnState] = None
        self._pending_msgs: list[str] = []
        #: Fire-and-forget control posts (stop/approval) — held strongly (a
        #: bare create_task result is only weakly referenced and can be
        #: GC-collected mid-flight, silently dropping a stop or a deny) and
        #: cancelled at teardown before the shared client closes.
        self._side_tasks: set[asyncio.Task] = set()
        #: True between a cancel frame and the turn's end — the engine answers
        #: a stop by erroring the stream, but that outcome is what the user
        #: asked for, so it must not surface as an error card (the runner eats
        #: its interrupt exception for the same reason).
        self._stop_requested = False
        self._main_task = asyncio.create_task(self._main_loop())

    # --- SandboxHandle contract ------------------------------------------

    async def wait(self) -> int:
        return await self._exit

    async def kill(self, *, grace_sec: float = 5.0) -> None:
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
        if self._main_task is not None:
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown is best-effort
                pass
        await self._close_resources()
        if not self._exit.done():
            self._exit.set_result(0)

    # --- teardown ---------------------------------------------------------

    async def _close_resources(self) -> None:
        """Everything a dying handle must release, shared by ``kill()`` and
        the main loop's own crash path: the in-flight turn, the control
        posts, the frame stream, and the HTTP client — so a crashed handle
        (which the manager replaces without ever calling ``kill``) leaks
        neither an SSE-consuming orphan turn nor a connection pool."""
        doomed = [t for t in [self._turn_task, *self._side_tasks] if t is not None and not t.done()]
        for task in doomed:
            task.cancel()
        for task in doomed:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown is best-effort
                pass
        self._side_tasks.clear()
        self.stdout.feed_eof()
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 - closing a client must not fail teardown
            logger.debug("kai engine handle: client close failed", exc_info=True)

    # --- auth --------------------------------------------------------------

    async def _bearer(self) -> str:
        """The session JWT, re-minted when its remaining life gets short.

        The mint is synchronous repo work (the ``kai_session`` credential row),
        so it runs off the event loop. Re-minting never revokes the previous
        token — both stay valid to their own ``exp``, the same coexistence the
        external ``/api/kai/sessions`` consumers rely on.
        """
        now = int(time.time())
        token = self._token
        if token is None or self._token_expires_at - now < _TOKEN_REFRESH_MARGIN_SECONDS:
            minted, self._token_expires_at = await asyncio.to_thread(self._mint, self._user_email, self._chat_id)
            self._token = minted
            return minted
        return token

    # --- frame consumption ---------------------------------------------------

    async def _main_loop(self) -> None:
        """Consume stdin frames; drive turns; keep cancel/approval live mid-turn.

        The queue is consumed continuously — a turn runs as its own task so a
        ``cancel`` or ``approval_decision`` arriving mid-stream acts on the
        in-flight turn instead of queueing behind it. ``user_msg`` frames that
        land mid-turn are buffered and sent one at a time afterwards (the
        engine 409s concurrent turns per chat), which is the same
        keep-for-after-the-turn ordering the in-sandbox runner applies.
        """
        get_task: "asyncio.Task | None" = None
        try:
            self.stdout.feed_frame({"type": "runner_ready"})
            if self._fail_reason:
                self._emit_fail_reason()
            while True:
                # ONE persistent get() across iterations, replaced only after
                # its item is consumed — never cancelled. Cancelling a pending
                # Queue.get each lap (the obvious shape) has a lost-wakeup
                # window: an item delivered between the wait() wake-up and the
                # cancel lands in the about-to-die task and is silently
                # dropped — a swallowed user message or cancel.
                if get_task is None:
                    get_task = asyncio.ensure_future(self._frames.get())
                waits = {get_task}
                if self._turn_task is not None:
                    waits.add(self._turn_task)
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                frame: Optional[dict] = None
                if get_task in done:
                    frame = get_task.result()
                    get_task = None
                if self._turn_task is not None and self._turn_task in done:
                    self._turn_task = None
                # Ordering when a frame and a turn-end land in the same wake:
                # control frames FIRST (a cancel/decision was aimed at the
                # turn that just ended — with it gone they no-op instead of
                # hitting the next turn), then the buffered next turn, then a
                # new user_msg (so it queues BEHIND the buffered one and send
                # order is preserved).
                if frame is not None and frame.get("type") != "user_msg":
                    self._dispatch(frame)
                if self._turn_task is None and self._pending_msgs:
                    self._start_turn(self._pending_msgs.pop(0))
                if frame is not None and frame.get("type") == "user_msg":
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("kai engine handle: main loop died for %s", self._chat_id)
            await self._close_resources()
            if not self._exit.done():
                # Non-zero: let the manager's crash-respawn rebuild the handle.
                self._exit.set_result(1)
        finally:
            # The persistent get() outlives the loop on cancellation — reap it
            # or it leaks as a forever-pending task on the queue.
            if get_task is not None and not get_task.done():
                get_task.cancel()

    def _emit_fail_reason(self) -> None:
        self.stdout.feed_frame({"type": "error", "kind": "engine_session_unusable", "message": self._fail_reason})
        self.stdout.feed_frame({"type": "done"})

    def _dispatch(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "user_msg":
            if self._fail_reason:
                # Dead handle: answer every attempt with the reason — the
                # boot-time copy can predate the client's WS seat, so this is
                # the delivery that reliably reaches a human.
                self._emit_fail_reason()
                return
            text = str(frame.get("text", ""))
            if self._turn_task is not None and not self._turn_task.done():
                self._pending_msgs.append(text)
            else:
                self._start_turn(text)
        elif kind == "cancel":
            if self._turn_task is not None and not self._turn_task.done():
                self._stop_requested = True
                self._spawn_side_task(self._post_stop())
        elif kind == "approval_decision":
            request_id = str(frame.get("request_id", ""))
            decision = str(frame.get("decision", ""))
            if request_id:
                self._spawn_side_task(self._post_approval(request_id, decision))
        # ticket_push (native egress credentials) has no engine meaning — the
        # engine mints its own per-turn tickets at /api/kai/tickets.

    def _spawn_side_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def _start_turn(self, text: str) -> None:
        self._stop_requested = False
        self._turn_task = asyncio.create_task(self._run_turn(text))

    # --- engine HTTP -----------------------------------------------------------

    async def _post_stop(self) -> None:
        try:
            token = await self._bearer()
            resp = await self._client.post(
                f"{self._base_url}/api/chat/{self._chat_id}/stop",
                headers={"Authorization": f"Bearer {token}"},
            )
            # `raise_for_status` as the approval post does: without it a 4xx/5xx
            # refusal reads as a successful stop, and the turn then hangs with
            # the composer locked until the SSE read timeout — the user pressed
            # Stop and nothing happened, with nothing in the log either.
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - a failed stop must not kill the handle
            logger.warning("kai engine handle: stop failed for %s", self._chat_id, exc_info=True)

    async def _post_approval(self, request_id: str, decision: str) -> None:
        """Forward a web approval decision, then resolve the card.

        ``request_id`` is the engine's ``toolCallId`` verbatim (that is what
        the approval_request frame carried). ``allow_session`` collapses to a
        plain allow — the engine's wire contract has no per-session grant.
        """
        approved = decision in ("allow", "allow_session")
        try:
            token = await self._bearer()
            resp = await self._client.post(
                f"{self._base_url}/api/chat/{self._chat_id}/approval",
                headers={"Authorization": f"Bearer {token}"},
                json={"toolUseId": request_id, "approved": approved},
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - resolution frame must still go out
            logger.warning("kai engine handle: approval post failed for %s", self._chat_id, exc_info=True)
            self.stdout.feed_frame(
                {
                    "type": "error",
                    "kind": "engine_approval_failed",
                    "message": "The approval decision could not be delivered to the engine.",
                }
            )
            return
        # Retire the card from the turn's own pending set so the engine's
        # later tool-output (or turn end) does not resolve it a second time
        # with a contradicting decision.
        state = self._turn_state
        if state is not None:
            state.pending_approvals.discard(request_id)
        self.stdout.feed_frame(
            {
                "type": "approval_resolved",
                "request_id": request_id,
                "decision": "deny" if not approved else decision,
            }
        )

    async def _run_turn(self, text: str) -> None:
        """One engine turn: POST the message, translate the SSE stream.

        Terminal discipline mirrors the runner: every path out of here emits
        ``done`` (the composer only unlocks on done/error/cancelled, and the
        manager clears its turn buffer on it), with the turn's accumulated
        text emitted as an ``assistant_message`` first whenever there is any —
        including engine-error and stalled-stream turns, where the partial the
        user already saw would otherwise vanish from history.
        """
        state = _TurnState()
        self._turn_state = state
        try:
            if self._stop_requested:
                # The cancel raced ahead of this turn even starting (user_msg
                # and cancel written back-to-back): the engine-side stop found
                # nothing to stop, so starting the turn now would run a whole
                # answer the user already cancelled.
                self._finish_turn(state)
                return
            token = await self._bearer()
            body = {
                "id": self._chat_id,
                "message": {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"type": "text", "text": text}],
                },
                # Route approvals through the native event + POST /approval
                # pair; without this the engine parks approvals on a UI
                # heuristic this transport does not implement. Advertised
                # even with approvals disabled — the kill-switch is applied
                # as an instant auto-deny per request (see _translate), the
                # same deny-with-a-message the native gate produces.
                "supportsApprovalRequestedEvent": True,
            }
            async with self._client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._emit_turn_failure(state, self._engine_error_message(resp))
                    return

                # SSE record assembly: consecutive `data:` lines belong to ONE
                # record, dispatched at the blank line (the CLI's SSE consumer
                # documents the same rules) — a proxy that reflows a long
                # payload across lines must not turn it into parse failures.
                def _dispatch_record(lines: list[str]) -> None:
                    payload = "\n".join(lines)
                    if not payload or payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        state.dropped_events += 1
                        return
                    if isinstance(event, dict):
                        self._translate(state, event)

                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
                        continue
                    if line.strip() and not line.startswith(":"):
                        continue  # id:/event: fields — not record boundaries
                    if not line.strip() and data_lines:
                        record, data_lines = data_lines, []
                        _dispatch_record(record)
                if data_lines:
                    # Stream closed mid-record (no trailing blank line): the
                    # final record still counts — dropping it here would lose
                    # whatever the engine said last.
                    _dispatch_record(data_lines)
        except asyncio.CancelledError:
            raise
        except httpx.ReadTimeout:
            self._emit_turn_failure(
                state,
                f"no engine activity for {int(_SSE_READ_TIMEOUT_SECONDS)}s; giving up on the turn",
            )
            return
        except Exception as exc:  # noqa: BLE001 - a failed turn ends, the handle survives
            logger.exception("kai engine turn failed for %s", self._chat_id)
            self._emit_turn_failure(state, f"engine turn failed: {exc}")
            return
        self._finish_turn(state)

    @staticmethod
    def _engine_error_message(resp: httpx.Response) -> str:
        try:
            detail = resp.json()
            message = detail.get("message") or detail.get("error") or resp.text
        except Exception:  # noqa: BLE001 - non-JSON error body
            message = resp.text
        return f"engine refused the turn ({resp.status_code}): {str(message)[:500]}"

    def _emit_turn_failure(self, state: _TurnState, message: str) -> None:
        self.stdout.feed_frame({"type": "error", "kind": "engine_error", "message": message})
        self._finish_turn(state)

    def _finish_turn(self, state: _TurnState) -> None:
        self._turn_state = None
        if state.dropped_events:
            # Loud, not fatal: a drifted event contract must not degrade into
            # a silently empty answer with nothing in the logs.
            logger.warning(
                "kai engine turn for %s dropped %d unparsable SSE payload(s)",
                self._chat_id,
                state.dropped_events,
            )
        # Cards the engine never resolved (stream died first) must not stay
        # pending forever — the manager only retires them on approval_resolved.
        for request_id in sorted(state.pending_approvals):
            self.stdout.feed_frame(
                {
                    "type": "approval_resolved",
                    "request_id": request_id,
                    "decision": "cancelled",
                    "reason": "the engine turn ended before this was answered",
                }
            )
        content = state.text()
        if content or state.tool_names:
            # tokens/model deliberately absent: the engine does not surface
            # usage on this stream. NOTE this is what leaves the manager's
            # token-derived caps unmetered for engine sessions (module
            # docstring, "Known limitations").
            self.stdout.feed_frame({"type": "assistant_message", "content": content})
        self.stdout.feed_frame({"type": "done"})

    # --- SSE → frame translation ------------------------------------------

    def _translate(self, state: _TurnState, event: dict) -> None:
        etype = str(event.get("type", ""))
        if etype == "text-delta":
            delta = str(event.get("delta", ""))
            if delta:
                state.parts.setdefault(str(event.get("id", "")), []).append(delta)
                self.stdout.feed_frame({"type": "token", "text": delta})
        elif etype == "tool-input-available":
            tool_call_id = str(event.get("toolCallId", ""))
            tool_name = str(event.get("toolName", "") or "tool")
            args = event.get("input") if isinstance(event.get("input"), dict) else {}
            state.tool_names[tool_call_id] = tool_name
            state.tool_args[tool_call_id] = args
            self.stdout.feed_frame(
                {
                    "type": "tool_call",
                    "id": tool_call_id,
                    "tool_use_id": tool_call_id,
                    "tool": tool_name,
                    "args": args,
                }
            )
        elif etype == "tool-approval-request":
            tool_call_id = str(event.get("toolCallId", ""))
            state.pending_approvals.add(tool_call_id)
            self.stdout.feed_frame(
                {
                    "type": "approval_request",
                    # The engine keys the decision on toolCallId, so the card's
                    # request_id IS the toolCallId — deliver_approval_decision
                    # hands it back verbatim.
                    "request_id": tool_call_id,
                    "tool": state.tool_names.get(tool_call_id, "tool"),
                    "command": json.dumps(state.tool_args.get(tool_call_id, {}), ensure_ascii=False)[:2000],
                    "reason": "The engine requires approval before running this tool.",
                    "timeout_seconds": self._approval_timeout_seconds,
                }
            )
            if not self._approvals_enabled:
                # Operator kill-switch (chat.approvals_enabled=false): the
                # native gate denies instantly with an actionable message
                # rather than letting tool calls wait — mirror it.
                self._spawn_side_task(self._post_approval(tool_call_id, "deny"))
        elif etype in ("tool-output-available", "tool-output-error"):
            tool_call_id = str(event.get("toolCallId", ""))
            if tool_call_id in state.pending_approvals:
                # Resolved engine-side (its own approval TTL, or a decision
                # this handle never saw) — retire the card either way.
                state.pending_approvals.discard(tool_call_id)
                self.stdout.feed_frame(
                    {
                        "type": "approval_resolved",
                        "request_id": tool_call_id,
                        "decision": "allow" if etype == "tool-output-available" else "deny",
                    }
                )
            if etype == "tool-output-available":
                output = event.get("output")
                result = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            else:
                result = str(event.get("errorText", "") or "tool failed")
            self.stdout.feed_frame(
                {
                    "type": "tool_result",
                    "id": tool_call_id,
                    "tool_use_id": tool_call_id,
                    # Runner parity: the frame's `tool` slot carries the
                    # pairing id (the manager's envelope overwrites `id`).
                    "tool": tool_call_id,
                    "result": result,
                    # The event type IS the verdict — forward it instead of
                    # making the client sniff the payload for a leading
                    # "error". A `tool-output-error` whose text begins
                    # anywhere else ("Catalog Error: Table … does not exist")
                    # was rendering with a success tick and folded shut.
                    "is_error": etype == "tool-output-error",
                }
            )
        elif etype == "error":
            if not self._stop_requested:
                self.stdout.feed_frame(
                    {
                        "type": "error",
                        "kind": "engine_error",
                        "message": str(event.get("errorText", "") or "engine error"),
                    }
                )
        # start / start-step / finish-step / finish / text-start / text-end /
        # reasoning-* / tool-input-start / tool-input-delta / data-*: no frame
        # equivalent (text segmentation is derived from the deltas' part ids).
        # `finish` in particular is redundant with the stream ending — the
        # terminal frames are emitted once, in _finish_turn, off stream end.


class KaiEngineProvider:
    """SandboxProvider whose sessions run on the embedded kai-agent engine."""

    #: The engine fetches this instance's workspace tarball itself
    #: (`HOST_WORKSPACE_URL` → GET /api/kai/workspace), so the manager must
    #: neither upload a workspace nor arm the sync sentinel.
    syncs_workspace = True
    #: The manager skips the NATIVE credential lifecycle — both the
    #: `_push_ticket_frame` mint (main/mcp/data_apps scopes nothing could
    #: redeem) and the paired scope-blind `revoke_session` sweeps (which
    #: would 401 the engine's own in-flight per-turn tickets on every
    #: resume/respawn). Engine sessions authenticate with the session JWT
    #: minted here; per-turn egress tickets are the engine's to mint and
    #: expire.
    provides_own_credentials = True

    def __init__(
        self,
        *,
        base_url: str,
        mint: Optional[Callable[[str, str], tuple[str, int]]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = base_url
        self._mint = mint or _default_mint
        self._transport = transport

    def _handle(self, *, chat_id: str, user_email: str, env: dict) -> KaiEngineHandle:
        try:
            timeout = int(str(env.get("AGNES_APPROVAL_TIMEOUT_SECONDS", "")) or _APPROVAL_TIMEOUT_FALLBACK_SECONDS)
        except ValueError:
            timeout = _APPROVAL_TIMEOUT_FALLBACK_SECONDS
        fail_reason: Optional[str] = None
        try:
            uuid.UUID(chat_id)
        except (ValueError, TypeError):
            # A pre-provider-switch session (`chat_<hex>` id) cannot key an
            # engine chat — the engine's uuid column rejects it. A DEAD handle
            # (error frame per message) is the legible surface: raising here
            # tears the WebSocket down with no user-facing explanation.
            # Do not assert WHY: the handle sees only the id shape. Before the
            # forks learned to honour a caller-owned id this also caught
            # brand-new co-drive sessions, and telling a user their
            # seconds-old conversation "predates" the provider sent them to
            # create another dead one.
            fail_reason = (
                "This conversation cannot run on the embedded engine — its id "
                "is not in the engine's format. Start a new conversation."
            )
        return KaiEngineHandle(
            chat_id=chat_id,
            user_email=user_email,
            base_url=self._base_url,
            mint=self._mint,
            transport=self._transport,
            approval_timeout_seconds=timeout,
            approvals_enabled=str(env.get("AGNES_APPROVALS", "on")).lower() != "off",
            fail_reason=fail_reason,
        )

    async def spawn(self, *, workdir: Path, env: dict, argv: list) -> KaiEngineHandle:
        return self._handle(
            chat_id=env.get("AGNES_SESSION_ID", ""),
            user_email=env.get("AGNES_USER_EMAIL", ""),
            env=env,
        )

    async def pause(self, handle: KaiEngineHandle) -> None:
        # Nothing to snapshot: the transcript lives in the engine's own store
        # and its sandbox lifecycle is its own. Tear the handle down; resume
        # rebuilds one for the same chat id.
        await handle.kill(grace_sec=0.0)

    async def resume(self, *, sandbox_id: str, runner_pid: int, env: dict) -> KaiEngineHandle:
        if not sandbox_id.startswith(_SANDBOX_ID_PREFIX):
            raise RuntimeError(f"not a kai-engine sandbox ref: {sandbox_id!r}")
        chat_id = sandbox_id[len(_SANDBOX_ID_PREFIX) :]
        # The manager passes the owner in env (the Protocol puts env on
        # resume for exactly this); the repo lookup is the fallback for a
        # caller that passes none — via the factory, so it follows whichever
        # backend the deployment runs.
        user_email = str(env.get("AGNES_USER_EMAIL", "") or "")
        if not user_email:
            from src.repositories import chat_session_repo

            session = await asyncio.to_thread(chat_session_repo().get_session, chat_id)
            if session is None:
                raise RuntimeError(f"no session row for {chat_id!r}")
            user_email = session.user_email
        return self._handle(chat_id=chat_id, user_email=user_email, env=env)

    async def keepalive(self, handle: KaiEngineHandle, *, timeout_seconds: int) -> None:
        return None

    async def destroy(self, *, sandbox_id: str) -> None:
        # No provider-side resource outlives a handle; the engine reaps its
        # own sandboxes.
        return None
