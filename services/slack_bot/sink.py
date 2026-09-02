"""Slack ↔ ChatManager pump bridge.

ChatManager.attach() reads frames off the runner subprocess and writes
them to a WebSocket via `ws.send_json({type: ...})`. For Slack DMs there
is no WebSocket — we want assistant_message frames forwarded to
`chat.postMessage` in the originating thread instead.

`SlackSinkBridge` is a duck-typed "WebSocket" that satisfies the manager's
contract (`.send_json`, `.receive_json`, `.close`) but routes frames to
`send_thread_reply`. Token / tool_call / housekeeping frames are dropped
(too chatty for Slack); only assistant_message, error, cancelled, and the
approval round-trip become visible chat posts.
"""

from __future__ import annotations

import asyncio
import logging

from app.chat.sources import strip_block, strip_next_actions_block
from services.slack_bot.blocks import (
    continue_on_web_block,
    new_session_block,
    stop_button_blocks,
)
from services.slack_bot.sender import (
    post_thread_reply_with_blocks,
    send_ephemeral,
    send_thread_reply,
    update_message,
)

logger = logging.getLogger(__name__)


class SlackSinkBridge:
    """Duck-typed WebSocket adapter for the ChatManager pump.

    Forwards `assistant_message` frames to Slack as a single
    `chat.postMessage` in the originating thread. Discards token / ready /
    runner_ready / tool_call / tool_result frames (too chatty for Slack);
    `error` and `cancelled` post visible thread messages so the user knows
    something happened, and `approval_request` posts the Continue-on-web
    nudge for a tool call waiting on a human (Slack renders no approve/deny
    card of its own — the decision comes back over the web WebSocket).

    Note the absence of ``supports_approvals``: this bridge is push-only
    for approvals, so ``ChatManager`` never counts it as a client that can
    answer one.
    """

    def __init__(
        self,
        *,
        channel: str,
        thread_ts: str,
        chat_id: str = "",
        owner: str = "",
        web_base: str = "",
    ) -> None:
        self._channel = channel
        self._thread_ts = thread_ts
        self._chat_id = chat_id
        self._owner = owner
        self._web_base = web_base
        self._closed = asyncio.Event()
        # ts of the current turn's button-bearing post, if any. Set on the
        # first assistant_message of a turn; cleared when the button is
        # stripped on cancelled / error / done (turn end).
        self._stop_msg_ts: str | None = None
        self._stop_msg_text: str = ""
        # request_ids this bridge posted an approval nudge for, so the
        # matching approval_resolved closes the loop (and an approval
        # answered on the web without a nudge stays silent here).
        self._pending_approvals: set[str] = set()

    def _turn_blocks(self, content: str) -> list[dict]:
        """Reply section + Stop + Continue-on-web (if web_base) + New-session.

        This is the producer that emits the interactive buttons onto every
        DM bot reply (spec §4 "everywhere a bot reply appears")."""
        blks = stop_button_blocks(text=content, chat_id=self._chat_id, owner=self._owner)
        link = continue_on_web_block(web_base=self._web_base, chat_id=self._chat_id)
        if link is not None:
            blks.append(link)
        blks.append(new_session_block(channel_id=self._channel, owner=self._owner))
        return blks

    async def send_json(self, data: dict) -> None:
        t = data.get("type")
        if t == "assistant_message":
            # Slack has no chips to draw the verdict into and no buttons wired
            # to the follow-up trailer, so both machine-readable fences would
            # just be code blocks of machinery under every answer. See
            # `app.chat.sources.strip_block` / `strip_next_actions_block`.
            content = strip_next_actions_block(strip_block(data.get("content", "")))
            if not content:
                return
            # With a chat_id we emit the interactive buttons on the streaming
            # reply and strip the Stop button at turn end. Without one, keep
            # the plain path (back-compat for callers that don't wire buttons).
            if self._chat_id and self._stop_msg_ts is None:
                ts = await post_thread_reply_with_blocks(
                    self._channel,
                    self._thread_ts,
                    content,
                    self._turn_blocks(content),
                )
                self._stop_msg_ts = ts
                self._stop_msg_text = content
            else:
                await send_thread_reply(self._channel, self._thread_ts, content)
        elif t == "error":
            kind = data.get("kind", "")
            msg = data.get("message", "")
            parts = [p for p in (kind, msg) if p]
            detail = ": ".join(parts)
            text = f":warning: {detail}" if detail else ":warning:"
            await send_thread_reply(self._channel, self._thread_ts, text)
            await self._strip_stop_button()
        elif t == "cancelled":
            await send_thread_reply(self._channel, self._thread_ts, "_(stopped)_")
            await self._strip_stop_button()
        elif t == "approval_request":
            await self._post_approval_request(data)
        elif t == "approval_resolved":
            await self._post_approval_resolved(data)
        elif t == "question_request":
            await self._post_question_request(data)
        elif t == "question_resolved":
            await self._post_question_resolved(data)
        elif t == "done":
            await self._strip_stop_button()
        # ready, runner_ready, token, tool_call, tool_result: silently ignored

    async def _post_approval_request(self, data: dict) -> None:
        """Tell the thread a tool call is waiting on a human, with the
        Continue-on-web button to go answer it.

        Slack has no approve/deny card of its own, so without this the turn
        would just stall silently until the gate times out. The manager
        stamps ``attended`` (see ``ChatManager._pump_subprocess_to_ws``):
        when a web client is already showing the card, stay quiet rather
        than nagging a user who is looking at the buttons.
        """
        if data.get("attended"):
            return
        self._pending_approvals.add(str(data.get("request_id", "")))
        command = str(data.get("command", "")).strip()
        reason = str(data.get("reason", "")).strip()
        lines = [":lock: *This command needs your approval*"]
        if command:
            # Fenced, and bounded well under Slack's per-block text cap —
            # the runner already truncates to 2000 chars, still far more
            # than belongs in a thread reply. Backticks are stripped first:
            # the command is agent-authored text, and one containing ``` would
            # otherwise close the fence and render the rest as live mrkdwn
            # (a crafted command could forge link text in the nudge the user
            # is about to act on).
            lines.append("```{}```".format(command[:400].replace("`", "'")))
        if reason:
            lines.append(reason)
        # Only promise a web hand-off when there IS one. Without a configured
        # public URL `continue_on_web_block` returns None, and telling someone
        # to "open the chat on the web" with no link is an instruction they
        # cannot act on — they then wait out the full approval timeout. Say
        # what will actually happen instead (Devin Review on #1157).
        if self._web_base:
            lines.append("Open the chat on the web to allow or deny it; it expires on its own if nobody answers.")
        else:
            lines.append(
                "This deployment has no public web URL configured, so there is nowhere to answer it — "
                "it will be denied when it times out. Ask an operator to set PUBLIC_URL "
                "(or server.public_url in instance.yaml)."
            )
        text = "\n".join(lines)
        link = continue_on_web_block(web_base=self._web_base, chat_id=self._chat_id)
        if link is None:
            await send_thread_reply(self._channel, self._thread_ts, text)
        else:
            await post_thread_reply_with_blocks(
                self._channel,
                self._thread_ts,
                text,
                [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, link],
            )

    async def _post_approval_resolved(self, data: dict) -> None:
        """Close the loop on a request this bridge announced. Silent for
        requests it never posted about (answered on the web while a client
        was attached), so the thread gains nothing it did not ask for."""
        request_id = str(data.get("request_id", ""))
        if request_id not in self._pending_approvals:
            return
        self._pending_approvals.discard(request_id)
        decision = str(data.get("decision", ""))
        text = {
            "allow": "_(approved)_",
            "allow_session": "_(approved for this session)_",
            "deny": "_(denied)_",
            "timeout": "_(approval expired — the command was not run)_",
            "unattended": "_(nobody could approve it — the command was not run)_",
        }.get(decision, "_(approval closed)_")
        await send_thread_reply(self._channel, self._thread_ts, text)

    async def _post_question_request(self, data: dict) -> None:
        """Nudge the thread that the agent is waiting on an AskUserQuestion
        answer, with the Continue-on-web button — the exact posture of
        ``_post_approval_request``: Slack renders no question card of its
        own, the answer comes back over the web WebSocket."""
        if data.get("attended"):
            return
        self._pending_approvals.add(str(data.get("request_id", "")))
        lines = [":question: *Agnes is asking you a question*"]
        for q in data.get("questions") or []:
            text = str((q or {}).get("question", "")).strip() if isinstance(q, dict) else ""
            if text:
                # Agent-authored text bound for mrkdwn: same backtick-stripping
                # posture as the approval nudge's command fence.
                lines.append("• {}".format(text[:400].replace("`", "'")))
        if self._web_base:
            lines.append("Open the chat on the web to answer; it expires on its own if nobody answers.")
        else:
            lines.append(
                "This deployment has no public web URL configured, so there is nowhere to answer it — "
                "the agent will continue without an answer when it times out. Ask an operator to set "
                "PUBLIC_URL (or server.public_url in instance.yaml)."
            )
        text = "\n".join(lines)
        link = continue_on_web_block(web_base=self._web_base, chat_id=self._chat_id)
        if link is None:
            await send_thread_reply(self._channel, self._thread_ts, text)
        else:
            await post_thread_reply_with_blocks(
                self._channel,
                self._thread_ts,
                text,
                [{"type": "section", "text": {"type": "mrkdwn", "text": text}}, link],
            )

    async def _post_question_resolved(self, data: dict) -> None:
        """Close the loop on a question this bridge announced — same
        silence rule as ``_post_approval_resolved``."""
        request_id = str(data.get("request_id", ""))
        if request_id not in self._pending_approvals:
            return
        self._pending_approvals.discard(request_id)
        decision = str(data.get("decision", ""))
        text = {
            "answered": "_(answered on the web)_",
            "dismissed": "_(dismissed without an answer)_",
            "timeout": "_(question expired — the agent continued without an answer)_",
            "unattended": "_(nobody could answer — the agent continued without an answer)_",
        }.get(decision, "_(question closed)_")
        await send_thread_reply(self._channel, self._thread_ts, text)

    async def _strip_stop_button(self) -> None:
        """Edit the turn's button-bearing post to remove the Stop button.

        Idempotent: a no-op once already stripped or if no button was posted.
        """
        if self._stop_msg_ts is None:
            return
        ts, text = self._stop_msg_ts, self._stop_msg_text
        self._stop_msg_ts = None
        self._stop_msg_text = ""
        await update_message(self._channel, ts, text, [])

    async def receive_json(self) -> dict:
        """The Slack surface is push-only from the manager's POV.

        Block until `close()` is called; then return a sentinel that lets
        the reader loop exit cleanly. The ChatManager only consumes
        `receive_json` from inside `app/api/chat.py::ws_stream`, not in
        `attach()`, so this is rarely exercised — but we implement it for
        API completeness.
        """
        await self._closed.wait()
        return {"type": "_closed"}

    async def close(self) -> None:
        self._closed.set()


class EphemeralCommandSink:
    """One-shot sink for slash commands.

    Posts the FIRST assistant_message of the turn to the caller's
    response_url, then ignores further frames. error/cancelled are also
    surfaced once — except a sender-limit refusal, which the command handler
    explains itself (see ``send_json``). Never stays attached — the session's
    permanent sink (web/DM) keeps streaming.
    """

    def __init__(self, *, response_url: str) -> None:
        self._response_url = response_url
        self._delivered = False
        self._closed = asyncio.Event()

    async def send_json(self, data: dict) -> None:
        if self._delivered:
            return
        t = data.get("type")
        if t == "assistant_message":
            content = strip_next_actions_block(strip_block(data.get("content", "")))
            if content:
                self._delivered = True
                await send_ephemeral(self._response_url, content)
        elif t == "error":
            from app.chat.manager import SENDER_LIMIT_FRAME_KINDS

            kind = data.get("kind", "")
            msg = data.get("message", "")
            self._delivered = True
            if kind in SENDER_LIMIT_FRAME_KINDS:
                # A sender-limit refusal (daily spend, conversation token
                # budget, message rate) reaches the slash-command user twice
                # otherwise: once here as the raw frame and once from the
                # handler's ``_send_or_explain_limit_ephemeral``, which catches
                # the same refusal's RuntimeError and posts the tailored
                # ``_SENDER_LIMIT_MESSAGES`` copy to this response_url. The
                # handler's copy wins; the turn is still over for this sink.
                return
            parts = [p for p in (kind, msg) if p]
            detail = ": ".join(parts)
            text = f":warning: {detail}" if detail else ":warning:"
            await send_ephemeral(self._response_url, text)
        elif t == "cancelled":
            self._delivered = True
            await send_ephemeral(self._response_url, "_(stopped)_")
        # ready / runner_ready / token / tool_call / tool_result / done: ignored

    async def receive_json(self) -> dict:
        await self._closed.wait()
        return {"type": "_closed"}

    async def close(self) -> None:
        self._closed.set()
