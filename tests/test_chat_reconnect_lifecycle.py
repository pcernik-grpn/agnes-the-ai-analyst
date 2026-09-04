"""A dropped chat socket recovers on its own, and says nothing while it does.

Connection state is not reported to the reader: while the sandbox is alive,
losing the stream and getting it back is backend bookkeeping. So `ws.onclose`
re-opens the same conversation itself — three attempts, exponential backoff,
silent — and only a recovery that ran out of attempts puts a line on screen.
The schedule and the "stay quiet" rule are mirrored from the Keboola UI chat
(`keboola/ui`, `packages/kai-chat`).

Guards below pin the parts that are easy to break from a distance:

  - the attempt budget and the 1 s / 2 s / 4 s schedule;
  - what refills the budget (an answer that COMPLETED, a submit, a switch to
    another conversation) and what deliberately does not (a socket that merely
    opened, i.e. the `ready` frame — refilling there lets a flapping
    connection retry forever);
  - the close-code branches: a superseded socket does nothing at all, 4404 is
    a rejection that skips the retries, a clean 1000 is nobody's problem, and
    everything else recovers;
  - a recovery already awaiting its ticket, superseded by a submit, must not
    close the replacement socket or spend the budget the submit just reset.

Static-source + node-executed guards against app/web/static/js/chat.js, the
same contract style as tests/test_chat_scroll_follow_ui.py — there is no
headless browser in CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")


def _read() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def _reconnect_block() -> str:
    """The reconnect helpers alone: the two constants sets, the budget, the
    generation, `_resetWsReconnect` and `_scheduleWsReconnect`."""
    js = _read()
    return js[js.index("const WS_RECONNECT_MAX_ATTEMPTS") : js.index("// --- capability empty-state panel")]


def _onclose_body() -> str:
    """`sock.onclose`'s body, from the active-socket guard to its close."""
    js = _read()
    start = js.index("  sock.onclose = (ev) => {")
    return js[start : js.index("\n  };", start)]


def _open_session_body() -> str:
    js = _read()
    return js[js.index("async function openSession(") : js.index("function handleFrame(frame)")]


def _node(script: str) -> dict:
    """Run `script` with the reconnect block in scope and return its JSON."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    harness = (
        # Fake clock: every scheduled retry is recorded and fired on demand.
        "const scheduled = [];\n"
        "const setTimeout = (fn, delay) => { scheduled.push({ fn, delay }); return scheduled.length; };\n"
        "const clearTimeout = (id) => { if (id) scheduled[id - 1].cleared = true; };\n"
        "const statuses = [];\n"
        "const setStatus = (text, kind) => statuses.push({ text, kind });\n"
        "const opened = [];\n"
        "let currentChatId = 'c1';\n"
        "let ws = null;\n"
        "const lastSeenSeqByChat = new Map();\n"
        "const console = { error: () => {} };\n"
        "let mint = async () => ({ ws_url: '/ws?ticket=t', turn_in_flight: false });\n"
        "const api = (path, init) => mint(path, init);\n"
        "const openSession = async (...args) => { opened.push(args); };\n"
        "const fire = async (i) => { const s = scheduled[i]; if (!s.cleared) await s.fn(); };\n"
        + _reconnect_block()
    )
    out = subprocess.run([node, "-e", harness + script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# The schedule and the budget
# --------------------------------------------------------------------------


def test_three_attempts_on_an_exponential_schedule_then_one_line():
    """1 s, 2 s, 4 s — and the fourth close is the reader's business."""
    res = _node(
        """
        // Four consecutive drops: each scheduled attempt fails to mint.
        mint = async () => { const e = new Error('offline'); throw e; };
        _scheduleWsReconnect('c1');
        await fire(0);
        await fire(1);
        await fire(2);
        process.stdout.write(JSON.stringify({
          delays: scheduled.map(s => s.delay),
          statuses,
        }));
        """
    )
    assert res["delays"] == [1000, 2000, 4000], "schedule must be 2^n x 1000 ms, three attempts"
    # Nothing said while retrying; exactly one line once the budget is spent.
    assert res["statuses"] == [
        {
            "text": "Could not get back to this conversation. Send your message again, or reload the page.",
            "kind": "error",
        }
    ]


def test_nothing_is_shown_while_a_retry_is_pending():
    res = _node(
        """
        _scheduleWsReconnect('c1');
        process.stdout.write(JSON.stringify({ statuses, pending: scheduled.length }));
        """
    )
    assert res["pending"] == 1
    assert res["statuses"] == [], "a recovery in flight must be invisible"


def test_a_reset_refills_the_budget():
    res = _node(
        """
        mint = async () => { throw new Error('offline'); };
        _scheduleWsReconnect('c1');
        await fire(0);
        await fire(1);
        await fire(2);              // budget spent, error shown
        _resetWsReconnect();        // the reader sends a message
        _scheduleWsReconnect('c1'); // ...and a drop can recover again
        process.stdout.write(JSON.stringify({ delays: scheduled.map(s => s.delay) }));
        """
    )
    assert res["delays"] == [1000, 2000, 4000, 1000], "a reset must restore the first-attempt delay"


def test_a_successful_attempt_reopens_the_same_conversation_with_the_ticket():
    res = _node(
        """
        mint = async () => ({ ws_url: '/ws?ticket=fresh', turn_in_flight: true });
        _scheduleWsReconnect('c1');
        lastSeenSeqByChat.set('c1', 42);
        await fire(0);
        process.stdout.write(JSON.stringify({
          opened,
          statuses,
          watermark: lastSeenSeqByChat.has('c1'),
        }));
        """
    )
    assert res["opened"] == [["c1", "/ws?ticket=fresh", {"reconnecting": True, "turnInFlight": True}]]
    assert res["statuses"] == [], "a recovery that worked says nothing"
    # History and replay are one source or the other — see the full_refresh
    # handler for the same reconciliation.
    assert res["watermark"] is False, "the replay watermark must be dropped before a transcript reload"


def test_a_mint_the_server_refuses_outright_fails_fast():
    """404/403/401 on the ticket: no fourth try changes that."""
    res = _node(
        """
        mint = async () => { const e = new Error('404'); e.status = 404; throw e; };
        _scheduleWsReconnect('c1');
        await fire(0);
        process.stdout.write(JSON.stringify({ delays: scheduled.map(s => s.delay), statuses }));
        """
    )
    assert res["delays"] == [1000], "a fatal mint must not schedule another attempt"
    assert res["statuses"][0]["kind"] == "error"


def test_a_transient_mint_failure_keeps_retrying():
    res = _node(
        """
        mint = async () => { const e = new Error('503'); e.status = 503; throw e; };
        _scheduleWsReconnect('c1');
        await fire(0);
        process.stdout.write(JSON.stringify({ delays: scheduled.map(s => s.delay), statuses }));
        """
    )
    assert res["delays"] == [1000, 2000]
    assert res["statuses"] == []


# --------------------------------------------------------------------------
# A recovery superseded mid-flight
# --------------------------------------------------------------------------


def test_a_retry_awaiting_its_ticket_is_invalidated_by_a_submit():
    """The reader submits while the ticket POST is in flight.

    `_resetWsReconnect` cannot cancel that request, and `currentChatId` still
    matches — only the generation tells the stale callback it is stale.
    """
    res = _node(
        """
        let release;
        mint = () => new Promise((r) => { release = () => r({ ws_url: '/ws?stale', turn_in_flight: false }); });
        _scheduleWsReconnect('c1');
        const pending = fire(0);          // starts the mint, then awaits it
        _resetWsReconnect();              // submitUserMessage's refill
        release();
        await pending;
        process.stdout.write(JSON.stringify({
          opened,
          statuses,
          delays: scheduled.map(s => s.delay),
        }));
        """
    )
    assert res["opened"] == [], "a superseded recovery must not re-open (it would close the live socket)"
    assert res["statuses"] == []
    assert res["delays"] == [1000], "and must not spend the budget the submit just reset"


def test_a_superseded_failing_retry_does_not_reschedule():
    res = _node(
        """
        let fail;
        mint = () => new Promise((_, rej) => { fail = () => rej(new Error('offline')); });
        _scheduleWsReconnect('c1');
        const pending = fire(0);
        _resetWsReconnect();
        fail();
        await pending;
        process.stdout.write(JSON.stringify({ delays: scheduled.map(s => s.delay), statuses }));
        """
    )
    assert res["delays"] == [1000]
    assert res["statuses"] == []


def test_a_retry_for_a_conversation_the_reader_left_is_dropped():
    res = _node(
        """
        _scheduleWsReconnect('c1');
        currentChatId = 'c2';
        await fire(0);
        process.stdout.write(JSON.stringify({ opened, statuses }));
        """
    )
    assert res["opened"] == []
    assert res["statuses"] == []


def test_a_socket_that_came_back_by_itself_is_left_alone():
    res = _node(
        """
        _scheduleWsReconnect('c1');
        ws = { readyState: 1 };   // a submit's ensureWsReady got there first
        await fire(0);
        process.stdout.write(JSON.stringify({ opened }));
        """
    )
    assert res["opened"] == []


# --------------------------------------------------------------------------
# The close handler's branches
# --------------------------------------------------------------------------


def test_only_the_active_socket_acts_on_its_own_close():
    body = _onclose_body()
    guard = body.index("if (ws !== sock) return;")
    for later in ("resetServerReady(", "setStatus(", "_scheduleWsReconnect("):
        assert guard < body.index(later), f"{later} must sit behind the active-socket guard"


def test_a_rejection_close_skips_the_retries_and_a_clean_close_says_nothing():
    body = _onclose_body()
    assert "if (WS_CLOSE_REJECTED.has(ev.code)) {" in body
    assert body.index("WS_CLOSE_REJECTED.has(ev.code)") < body.index("_scheduleWsReconnect(")
    assert 'setStatus(WS_RECONNECT_FAILED_COPY, "error");' in body
    # A clean close is neither retried nor reported.
    assert "if (ev.code === 1000) return;" in body
    assert body.index("if (ev.code === 1000) return;") < body.index("_scheduleWsReconnect(")


def test_the_rejection_set_holds_only_codes_a_browser_can_see():
    """4401/4503 are sent BEFORE ws.accept() — a handshake rejection, which
    reaches the browser as an abnormal 1006 and never as the code itself. A
    branch on them would never fire; the ticket HTTP status is where those
    failures are legible, and WS_MINT_FATAL_STATUS is that branch."""
    block = _reconnect_block()
    assert "const WS_CLOSE_REJECTED = new Set([4404]);" in block
    assert "const WS_MINT_FATAL_STATUS = new Set([401, 403, 404]);" in block


def test_an_unexpected_close_still_recovers():
    body = _onclose_body()
    assert "_scheduleWsReconnect(chatId);" in body, "an ordinary drop must reconnect, not merely go quiet"


# --------------------------------------------------------------------------
# What refills the budget, and what must not
# --------------------------------------------------------------------------


def test_a_completed_answer_refills_the_budget_and_a_bare_socket_does_not():
    js = _read()
    frames = js[js.index("function handleFrame(frame)") :]
    assistant = frames.index('case "assistant_message":')
    assert "_wsReconnectAttempts = 0;" in frames[assistant : assistant + 700]
    # The `ready` / `runner_ready` arm must NOT refill it: a socket that
    # merely opened proves nothing, and a flapping connection would then
    # retry forever.
    ready = frames[frames.index('case "ready":') : frames.index('case "token":')]
    assert "_wsReconnectAttempts" not in ready
    assert "_resetWsReconnect" not in ready


def test_sending_a_message_refills_the_budget_before_the_socket_wait():
    js = _read()
    submit = js[js.index("async function submitUserMessage(text)") :]
    reset = submit.index("_resetWsReconnect();")
    assert reset < submit.index("await ensureWsReady();"), "the refill must precede the reconnect it enables"


def test_switching_conversations_starts_with_its_own_budget():
    body = _open_session_body()
    assert "if (_switchingSession) _resetWsReconnect();" in body


# --------------------------------------------------------------------------
# Readiness waiters survive a same-session reconnect
# --------------------------------------------------------------------------


def test_an_unresolved_ready_gate_is_bridged_across_a_reconnect():
    """A submit already awaiting `serverReadyPromise` when the socket drops
    must be released by the reconnect's `ready` frame, not left to time out."""
    js = _read()
    fn = js[js.index("function resetServerReady(chatId = null) {") :]
    fn = fn[: fn.index("\n}")]
    assert "!_serverReadySettled" in fn and "chatId === _serverReadyChatId" in fn
    # Both re-arms name the conversation, or there is nothing to compare.
    assert "resetServerReady(chatId);" in _onclose_body()
    assert "resetServerReady(chatId);" in _open_session_body()


def test_a_conversation_switch_never_bridges():
    """Session B's `ready` must not release a submit aimed at session A."""
    js = _read()
    fn = js[js.index("function resetServerReady(chatId = null) {") :]
    fn = fn[: fn.index("\n}")]
    assert "chatId !== null" in fn, "a call that names no conversation must always re-arm"


def test_the_ready_frame_marks_the_gate_settled():
    js = _read()
    frames = js[js.index("function handleFrame(frame)") :]
    ready = frames[frames.index('case "ready":') : frames.index('case "token":')]
    assert "_serverReadySettled = true;" in ready
    assert ready.index("_serverReadySettled = true;") < ready.index("if (resolveServerReady) resolveServerReady();")
