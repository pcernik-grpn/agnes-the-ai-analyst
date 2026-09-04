"""The activity indicator lives exactly as long as the turn does (#2156).

Before this, the three dots were a submit-only affordance: painted on send,
removed by the first server frame, never rendered again. The Stop button lived
until a terminal frame, off twelve separate `hidden` assignments. So for the
body of a multi-tool turn the only thing on screen disagreeing with "this
answer is finished" was a button in the composer nobody watches while reading
— and a partial answer read as final is the text people quote onward.

Both surfaces now derive from one `_turnInFlight`, so the guards below are
mostly about that derivation holding:

  - a submit paints the dots on the same tick, and the first token hands the
    signal over to the streaming caret;
  - a SETTLED tool result brings the dots back while the turn runs — the gap
    the report is actually about — but only after a settle delay, so the
    common fast hop between frames does not flicker a bubble into the foot of
    the transcript;
  - a running tool card, a streaming bubble, and an open approval/question
    card each suppress the dots, the last because a turn waiting on the reader
    must never be dressed up as the agent making progress;
  - the #1973 contract survives: a `ready` verdict may call off a turn a
    REATTACH guessed at, never one this tab submitted — including after the
    placeholder has been cleared and re-shown many times inside that turn,
    which is the trap that made the old `clearThinkingPlaceholder` reset wrong;
  - Stop and the indicator cannot disagree, because one function writes both.

Node-executed guards against the real block in app/web/static/js/chat.js, plus
static guards for the wiring the block cannot see (handleFrame's tail, and the
single remaining writer of the Stop button) — the same contract style as
tests/test_chat_reconnect_lifecycle.py. There is no headless browser in CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _read() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def _collapse_block() -> str:
    """`_collapseFinishedToolCalls` alone — the per-turn cleanup that has to
    stop an interrupted call claiming it is still running."""
    js = _read()
    start = js.index("function _collapseFinishedToolCalls() {")
    return js[start : js.index("\n/** Heuristic: a stringified tool error", start)]


def _activity_block() -> str:
    """The turn-activity block alone: the state, its writer, the derivation
    and the placeholder pair."""
    js = _read()
    start = js.index("// ---------- Turn activity: one state, two surfaces")
    return js[start : js.index("\n// Streaming state — captured per turn")]


HARNESS = """
// --- fake DOM ------------------------------------------------------------
const cancelBtn = { hidden: true };
const messages = { kids: [], appendChild(el) { this.kids.push(el); } };
const $ = (id) =>
  id === "cancel-btn" ? cancelBtn
  : id === "chat-messages" ? messages
  : id === "chat-jump-latest" ? jumpBtn
  : null;
const createMessageShell = () => {
  const body = { innerHTML: "" };
  const el = {
    classList: { add() {} },
    querySelector: () => body,
    remove() { const i = messages.kids.indexOf(el); if (i >= 0) messages.kids.splice(i, 1); },
  };
  return el;
};
const maybeScrollToBottom = () => {};
const jumpBtn = { classes: new Set(), classList: {
  toggle(name, on) { if (on) jumpBtn.classes.add(name); else jumpBtn.classes.delete(name); },
} };

// --- the state the derivation reads -------------------------------------
let currentAssistantArticle = null;   // a streaming bubble (carries its own caret)
const inFlightToolCalls = new Map();  // running tool cards
/** Approval / question cards still marked is-running. Modelled as real
 *  objects so the turn-end cleanup can operate on them. */
let decisionCards = [];
const addDecisionCard = () => {
  const controls = [{ disabled: false }, { disabled: false }];
  const card = {
    classes: new Set(["is-running"]),
    classList: { remove(n) { card.classes.delete(n); } },
    querySelectorAll: () => controls,
    controls,
  };
  decisionCards.push(card);
  return card;
};
const _openDecisionCards = () => decisionCards.filter((c) => c.classes.has("is-running"));
const document = {
  querySelector: () => _openDecisionCards()[0] || null,
  querySelectorAll: () => _openDecisionCards(),
};

// --- fake clock ---------------------------------------------------------
const timers = [];
const setTimeout = (fn, delay) => { timers.push({ fn, delay }); return timers.length; };
const clearTimeout = (id) => { if (id) timers[id - 1].cleared = true; };
/** Fire every armed, uncleared timer — i.e. let the settle delay elapse. */
const elapse = () => {
  for (const t of timers) {
    if (!t.cleared && !t.fired) { t.fired = true; t.fn(); }
  }
};
const armed = () => timers.filter((t) => !t.cleared && !t.fired).map((t) => t.delay);

// --- helpers ------------------------------------------------------------
const iconEl = (name) => ({ name });
let _currentTurnToolCards = [];
const _updateToolGroupSummary = () => {};
const _endToolGroup = () => {};
const _resetFactsTurnEvidence = () => {};
/** A tool card real enough for the collapse pass: status class, icon slot,
 *  meta line. */
const startTool = (id) => {
  const icon = { kids: [], replaceChildren(...k) { icon.kids = k; } };
  const meta = { textContent: "running…" };
  const card = {
    open: true,
    classes: new Set(["is-running"]),
    classList: { remove(n) { card.classes.delete(n); } },
    querySelector: (sel) => (sel === ".cloud-chat-tool-icon" ? icon : sel === ".cloud-chat-tool-meta" ? meta : null),
    closest: () => null,
    icon, meta,
  };
  inFlightToolCalls.set(id, card);
  _currentTurnToolCards.push(card);
  return card;
};
const settleTool = (id) => { inFlightToolCalls.delete(id); };
const snap = () => ({
  dots: thinkingEl !== null,
  onScreen: messages.kids.length,
  stop: cancelBtn.hidden === false,
  turn: _turnInFlight,
  reattachGuess: _reattachGuessedTurn,
  working: jumpBtn.classes.has("is-working"),
  armed: armed(),
});
const out = (o) => process.stdout.write(JSON.stringify(o));
"""


def _node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "-e", HARNESS + _activity_block() + "\n" + _collapse_block() + "\n" + script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# The turn's two ends
# --------------------------------------------------------------------------


def test_a_submit_paints_the_dots_on_the_same_tick():
    """No settle delay on send: the keypress has to be acknowledged now."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        out({ ...snap() });
        """
    )
    assert res["dots"] is True, "the dots must be up before any timer elapses"
    assert res["stop"] is True
    assert res["armed"] == [], "an immediate paint must not also arm the settle timer"


def test_the_first_token_hands_the_signal_to_the_streaming_caret():
    """The dots come down, but the TURN does not — this is where the old code
    stopped having anything to say."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = {};      // a token opened the bubble
        syncActivityIndicator();
        out({ ...snap() });
        """
    )
    assert res["dots"] is False, "a streaming bubble carries its own caret"
    assert res["stop"] is True and res["turn"] is True, "the turn is still running"


def test_a_terminal_frame_clears_both_at_once_and_never_on_a_delay():
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        setTurnInFlight(false);
        out({ ...snap() });
        """
    )
    assert res == {
        "dots": False,
        "onScreen": 0,
        "stop": False,
        "turn": False,
        "reattachGuess": False,
        "working": False,
        "armed": [],
    }


# --------------------------------------------------------------------------
# The gap the report is about
# --------------------------------------------------------------------------


def test_a_settled_tool_result_brings_the_indicator_back():
    """The reported bug, in one test: text sealed, tool done, agent thinking,
    nothing on screen moving."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = {};
        syncActivityIndicator();                 // tokens streaming
        currentAssistantArticle = null;          // tool_call sealed the segment
        startTool("t1");
        syncActivityIndicator();                 // card running
        const during = snap();
        settleTool("t1");                        // tool_result: card settles
        syncActivityIndicator();
        const beforeSettle = snap();
        elapse();
        out({ during, beforeSettle, after: snap() });
        """
    )
    assert res["during"]["dots"] is False, "a running card already says it"
    assert res["beforeSettle"]["dots"] is False, "not on the same tick — that is a flicker"
    assert res["beforeSettle"]["armed"] == [400], "the paint is armed, not painted"
    assert res["after"]["dots"] is True, "and lands once the transcript has really gone quiet"
    assert res["after"]["stop"] is True, "Stop and the dots agree throughout"


def test_a_fast_hop_between_frames_never_flickers():
    """tool_result → next token is often tens of ms. Nothing may be painted."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = null;
        startTool("t1");
        syncActivityIndicator();
        settleTool("t1");
        syncActivityIndicator();                 // quiet: settle armed
        currentAssistantArticle = {};            // the next token lands inside it
        syncActivityIndicator();
        elapse();
        out({ ...snap(), everPainted: messages.kids.length });
        """
    )
    assert res["dots"] is False
    assert res["everPainted"] == 0, "no bubble may ever reach the transcript"


def test_the_settle_timer_re_asks_instead_of_trusting_its_own_arming():
    """Belt and braces for the same hop when the state changes without a
    sync call in between."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = null;
        startTool("t1"); syncActivityIndicator(); settleTool("t1");
        syncActivityIndicator();       // arms the paint
        startTool("t2");               // a new call starts, no sync
        elapse();
        out({ ...snap() });
        """
    )
    assert res["dots"] is False, "the timer must re-derive when it fires"


# --------------------------------------------------------------------------
# What must NOT read as the agent working
# --------------------------------------------------------------------------


def test_an_open_decision_card_is_not_progress():
    """An approval or question card is the turn waiting on THIS READER."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = null;
        const card = addDecisionCard();          // approval card on screen
        syncActivityIndicator();
        elapse();
        const waiting = snap();
        card.classes.delete("is-running");       // answered; the agent resumes
        syncActivityIndicator();
        elapse();
        out({ waiting, resumed: snap() });
        """
    )
    assert res["waiting"]["dots"] is False, "waiting on a human is not progress"
    assert res["waiting"]["stop"] is True, "but the turn is still stoppable"
    assert res["resumed"]["dots"] is True, "and once answered the agent is working again"


def test_nothing_is_painted_when_no_turn_is_running():
    res = _node(
        """
        currentAssistantArticle = null;
        syncActivityIndicator();
        elapse();
        out({ ...snap() });
        """
    )
    assert res["dots"] is False and res["stop"] is False


# --------------------------------------------------------------------------
# #1973 survives: whose turn is the `ready` frame allowed to call off?
# --------------------------------------------------------------------------


def test_ready_may_call_off_a_reattachs_guess():
    res = _node(
        """
        _reattachGuessedTurn = true;
        setTurnInFlight(true, { immediate: true });
        // ready { turn_in_flight: false }
        if (_reattachGuessedTurn) setTurnInFlight(false);
        out({ ...snap() });
        """
    )
    assert res["turn"] is False and res["dots"] is False and res["stop"] is False


def test_ready_may_not_call_off_a_submits_turn():
    """A `ready` can arrive before the server has even received the message."""
    res = _node(
        """
        _reattachGuessedTurn = false;
        setTurnInFlight(true, { immediate: true });
        if (_reattachGuessedTurn) setTurnInFlight(false);   // must not fire
        out({ ...snap() });
        """
    )
    assert res["turn"] is True and res["dots"] is True and res["stop"] is True


def test_clearing_the_placeholder_no_longer_forgets_whose_turn_it_is():
    """The trap the new model would otherwise have walked into.

    `clearThinkingPlaceholder` used to reset the reattach flag, which was fine
    while the placeholder was painted once per turn. It now comes and goes on
    every token and every tool call, so resetting there would let a late
    `ready` frame call off a turn a reattach legitimately owns.
    """
    res = _node(
        """
        _reattachGuessedTurn = true;
        setTurnInFlight(true, { immediate: true });
        // A live reattached turn: bubble, tool call, quiet, bubble again.
        currentAssistantArticle = {}; syncActivityIndicator();
        currentAssistantArticle = null; startTool("t1"); syncActivityIndicator();
        settleTool("t1"); syncActivityIndicator(); elapse();
        currentAssistantArticle = {}; syncActivityIndicator();
        out({ ...snap() });
        """
    )
    assert res["reattachGuess"] is True, "the turn is still the reattach's to call off"


def test_a_stopped_turn_drops_the_reattach_claim():
    res = _node(
        """
        _reattachGuessedTurn = true;
        setTurnInFlight(true, { immediate: true });
        setTurnInFlight(false);
        out({ ...snap() });
        """
    )
    assert res["reattachGuess"] is False


# --------------------------------------------------------------------------
# The invariant the report asked for, stated directly
# --------------------------------------------------------------------------


def test_stop_and_the_indicator_can_never_disagree():
    """Across a whole multi-tool turn, `stop` is true exactly while the turn
    is, and the dots are up exactly when nothing else is saying so."""
    res = _node(
        """
        const trace = [];
        const step = (label) => trace.push({ label, dots: thinkingEl !== null, stop: !cancelBtn.hidden });
        setTurnInFlight(true, { immediate: true });               step("submit");
        currentAssistantArticle = {}; syncActivityIndicator();    step("streaming");
        currentAssistantArticle = null; startTool("t1"); syncActivityIndicator(); step("tool running");
        settleTool("t1"); syncActivityIndicator(); elapse();      step("thinking");
        currentAssistantArticle = {}; syncActivityIndicator();    step("streaming again");
        currentAssistantArticle = null; syncActivityIndicator(); elapse(); step("turn close");
        setTurnInFlight(false);                                   step("done");
        out({ trace });
        """
    )
    assert res["trace"] == [
        {"label": "submit", "dots": True, "stop": True},
        {"label": "streaming", "dots": False, "stop": True},
        {"label": "tool running", "dots": False, "stop": True},
        {"label": "thinking", "dots": True, "stop": True},
        {"label": "streaming again", "dots": False, "stop": True},
        {"label": "turn close", "dots": True, "stop": True},
        {"label": "done", "dots": False, "stop": False},
    ]
    # The point of the whole change: Stop is up for every step but the last.
    assert [s["stop"] for s in res["trace"]] == [True] * 6 + [False]


def test_the_way_back_carries_the_turn_state_for_a_reader_who_scrolled_up():
    """The foot of the transcript is exactly what a scrolled-up reader cannot
    see, so the "Jump to latest" button is marked for as long as the turn
    runs — including while the button itself is hidden, so scrolling up
    mid-turn reveals one already carrying the pulse."""
    res = _node(
        """
        const idle = snap();
        setTurnInFlight(true, { immediate: true });
        const running = snap();
        currentAssistantArticle = {}; syncActivityIndicator();
        const streaming = snap();          // dots down, turn still up
        setTurnInFlight(false);
        out({ idle, running, streaming, stopped: snap() });
        """
    )
    assert res["idle"]["working"] is False
    assert res["running"]["working"] is True
    assert res["streaming"]["working"] is True, (
        "the pulse tracks the TURN, not the dots — it must outlive the first frame"
    )
    assert res["stopped"]["working"] is False


# --------------------------------------------------------------------------
# Static guards: the wiring the block cannot see
# --------------------------------------------------------------------------


def test_setTurnInFlight_is_the_only_writer_of_the_stop_button():
    """The twelve scattered `hidden` assignments are what let the two surfaces
    drift apart. Exactly one may remain, inside setTurnInFlight."""
    js = _read()
    writers = [
        line.strip()
        for line in js.split("\n")
        if "cancel-btn" in line or ("cancelBtn" in line and ".hidden" in line)
    ]
    assignments = [w for w in writers if ".hidden" in w and "=" in w]
    assert assignments == ["if (cancelBtn) cancelBtn.hidden = !_turnInFlight;"], (
        "the Stop button's visibility must be written in exactly one place "
        f"(setTurnInFlight); found: {assignments}"
    )


def test_every_frame_re_derives_the_indicator():
    """The derivation is driven from handleFrame's tail, after the switch —
    not per-case, which is how the one show and eight clears drifted apart."""
    js = _read()
    body = js[js.index("function handleFrame(frame)") :]
    body = body[: body.index("\n}\n") + 3]
    assert "syncActivityIndicator();" in body, "handleFrame must re-derive the indicator"
    tail = body[body.rindex("  }") :]
    assert "syncActivityIndicator();" in tail, (
        "the re-derivation must sit AFTER the switch, so a half-applied state "
        "(segment sealed, tool card not yet appended) is never painted"
    )
    assert "return" not in body[body.index("switch (frame.type)") : body.rindex("  }")], (
        "a `return` inside the switch would skip the re-derivation"
    )


def test_the_jump_button_pulse_survives_reduced_motion_as_a_static_dot():
    css = CHAT_CSS.read_text(encoding="utf-8")
    assert ".cloud-chat-jump.is-working::before" in css
    reduced = css[css.index(".cloud-chat-jump.is-working::before") :]
    reduced = reduced[reduced.index("prefers-reduced-motion") :]
    block = reduced[: reduced.index("}")]
    assert "animation: none" in block, "reduced motion drops the pulse"
    assert "display: none" not in block and "content: none" not in block, (
        "reduced motion must keep the DOT — it is the signal, not the decoration"
    )


# --------------------------------------------------------------------------
# An interrupted call must not silence every later turn
# --------------------------------------------------------------------------


def test_an_interrupted_call_stops_claiming_it_is_running():
    """A turn cancelled with a tool still out used to leave the card on
    "running…" and the in-flight map holding an entry nobody would ever
    delete. The map is what makes this #2156's problem: the derivation reads
    it, so one interrupted call would suppress the indicator for the rest of
    the conversation."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        const card = startTool("t1");
        syncActivityIndicator();
        // The turn is cancelled while the tool is still out.
        _collapseFinishedToolCalls();
        setTurnInFlight(false);
        out({
          stillRunning: card.classes.has("is-running"),
          meta: card.meta.textContent,
          icon: card.icon.kids.map((k) => k.name),
          inFlight: inFlightToolCalls.size,
        });
        """
    )
    assert res["stillRunning"] is False, "the card must stop claiming a running call"
    assert res["meta"] == "did not finish"
    assert res["icon"] == ["ban"], "stopped, not failed — the call did not error"
    assert res["inFlight"] == 0, "the per-turn map resets where the turn ends"


def test_the_next_turn_after_an_interrupted_call_still_shows_the_indicator():
    """The regression the cleanup exists to prevent."""
    res = _node(
        """
        // Turn 1: cancelled with a tool still out.
        setTurnInFlight(true, { immediate: true });
        startTool("t1");
        syncActivityIndicator();
        _collapseFinishedToolCalls();
        setTurnInFlight(false);
        // Turn 2: a fresh question, and the agent goes quiet to think.
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = {}; syncActivityIndicator();
        currentAssistantArticle = null; syncActivityIndicator();
        elapse();
        out({ ...snap() });
        """
    )
    assert res["dots"] is True, (
        "a stale in-flight entry from a previous turn must not suppress the "
        "indicator for the rest of the conversation"
    )


def test_a_decision_card_the_turn_died_under_stops_awaiting_an_answer():
    """The same leak on the other surface. The server normally resolves these
    itself, so this is the belt and braces: an unresolved card keeps the
    `is-running` class the derivation reads, and would silence the indicator
    for the rest of the conversation."""
    res = _node(
        """
        setTurnInFlight(true, { immediate: true });
        const card = addDecisionCard();
        syncActivityIndicator();
        _collapseFinishedToolCalls();       // the turn errors out under it
        setTurnInFlight(false);
        const dead = { running: card.classes.has("is-running"),
                       enabled: card.controls.filter((c) => !c.disabled).length };
        // A new turn goes quiet to think.
        setTurnInFlight(true, { immediate: true });
        currentAssistantArticle = {}; syncActivityIndicator();
        currentAssistantArticle = null; syncActivityIndicator();
        elapse();
        out({ dead, next: snap() });
        """
    )
    assert res["dead"]["running"] is False, "it is not awaiting a decision any more"
    assert res["dead"]["enabled"] == 0, "its buttons answer a turn nobody is listening to"
    assert res["next"]["dots"] is True, "and the next turn's indicator is not silenced"


def test_an_unrecoverable_socket_takes_the_signal_down():
    """No frame can arrive on a socket we have stopped re-opening, so no
    terminal frame is coming. Left alone, deriving the indicator from the turn
    state would have turned an already-wrong stale Stop button into a spinner
    running forever under an abandoned answer.

    Static guard: `_scheduleWsReconnect` lives outside the extracted block
    (it depends on the whole reconnect apparatus), so this pins the wiring the
    node harness cannot reach.
    """
    js = _read()
    spent = js[js.index("function _scheduleWsReconnect(chatId) {") :]
    spent = spent[: spent.index("const delay =")]
    assert "setTurnInFlight(false);" in spent, (
        "a spent reconnect budget must stop the turn reading as running — "
        "no terminal frame can arrive to do it"
    )
