"""Scrolling up during a turn (follow-up to #2049).

A turn writes continuously, and every append asked to be scrolled into view.
The old rule re-derived "should I follow?" from the scroll position alone, with
a 320px slack zone — so a reader who scrolled up one wheel notch (~100px) to
re-read something was put back at the floor by the next token, and a long
answer was unreadable until it finished.

The rule is an explicit verdict now: only the READER's own scrolling changes
whether the stream follows. Scrolling up stops it at any distance; arriving
back at the floor starts it again. Guards below pin both halves, plus the two
places that legitimately re-arm it (submit, fresh transcript) and the one
scroll this file performs that can move UP (the composer growing).

Static-source + node-executed guards against app/web/static/js/chat.js, the
same contract style as tests/test_chat_tool_rendering_ui.py — there is no
headless browser in CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
CHAT_HTML = Path("app/web/templates/chat.html")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _follow_section() -> str:
    """The smart auto-scroll block, minus its DOM wiring IIFE."""
    js = _read(CHAT_JS)
    return js[js.index("const SCROLL_STICK_PX") : js.index("(function wireStreamFollow()")]


def _gesture_predicate() -> str:
    """`gestureCanScrollTranscriptUp` alone, with the DOM it reaches for faked."""
    js = _read(CHAT_JS)
    fn = js[js.index("function gestureCanScrollTranscriptUp(") : js.index("/** The way back.")]
    return (
        "class Element { get parentElement() { return this._parent || null; } }\n"
        "const getComputedStyle = (n) => ({ overflowY: n._overflowY || 'visible' });\n"
        "const node = (o) => Object.assign(new Element(), "
        "{ scrollTop: 0, scrollHeight: 0, clientHeight: 0 }, o);\n" + fn
    )


# The harness stands in for the two elements the section reaches for. `top`
# is writable exactly as `scrollTop` is, so a "scroll" is a write plus the
# event the browser would fire.
_HARNESS = """
// scrollTop CLAMPS to [0, scrollHeight - clientHeight], which is why
// `el.scrollTop = el.scrollHeight` lands on the floor rather than past it —
// the code under test relies on that, so the fake has to do it too.
let _top = 4000;
const msgs = {
  scrollHeight: 5000,
  clientHeight: 1000,
  get scrollTop() { return _top; },
  set scrollTop(v) { _top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); },
};
const floor = () => msgs.scrollHeight - msgs.clientHeight;
const jump = { hidden: true };
const $ = (id) => (id === "chat-messages" ? msgs : id === "chat-jump-latest" ? jump : null);
// A reader's gesture: the offset moves, then the frame's scroll event fires.
const scrollTo = (top) => { msgs.scrollTop = top; onMessagesScroll(); };
const grow = (px) => { msgs.scrollHeight += px; };   // fires no scroll event
"""


def _run(body: str) -> dict:
    script = _HARNESS + _follow_section() + body
    return json.loads(_node_run(script))


# ── the verdict follows the reader, not the position ─────────────────────────


def test_a_reader_at_the_floor_is_followed():
    res = _run("""
      grow(400);
      maybeScrollToBottom();
      process.stdout.write(JSON.stringify({ top: msgs.scrollTop, floor: floor() }));
    """)
    assert res["top"] == res["floor"], "a turn writing under a reader at the floor keeps up"


def test_one_wheel_notch_up_stops_the_stream_following():
    """The regression this exists for: 50px is well inside the old 320px slack
    zone, so every scroll of that size was undone by the next token."""
    res = _run("""
      scrollTo(4000);            // at the floor (5000 - 1000)
      scrollTo(3950);            // up one notch
      const stuckAfterScroll = _stickToBottom;
      grow(300);
      maybeScrollToBottom();
      process.stdout.write(JSON.stringify({ stuckAfterScroll, top: msgs.scrollTop }));
    """)
    assert res["stuckAfterScroll"] is False
    assert res["top"] == 3950, "the reader stays exactly where they put themselves"


def test_content_growing_below_never_moves_a_reader_who_scrolled_away():
    res = _run("""
      scrollTo(4000);
      scrollTo(1000);            // well up the transcript
      for (let i = 0; i < 40; i++) { grow(120); maybeScrollToBottom(); }
      process.stdout.write(JSON.stringify({ top: msgs.scrollTop, height: msgs.scrollHeight }));
    """)
    assert res["top"] == 1000
    assert res["height"] == 5000 + 40 * 120, "the turn kept writing; only the view stayed put"


def test_scrolling_back_to_the_floor_resumes_following_without_a_click():
    res = _run("""
      scrollTo(4000);
      scrollTo(2000);            // away
      grow(1000);                // height 6000, floor 5000
      const away = _stickToBottom;
      scrollTo(4950);            // back within SCROLL_STICK_PX of the floor
      const back = _stickToBottom;
      grow(200);
      maybeScrollToBottom();
      process.stdout.write(JSON.stringify({ away, back, top: msgs.scrollTop, floor: floor() }));
    """)
    assert res["away"] is False
    assert res["back"] is True, "arriving at the floor IS the request to follow"
    assert res["top"] == res["floor"]


def test_the_slack_zone_only_applies_on_the_way_back_down():
    """Asymmetric on purpose. Downward into the zone re-arms; upward INTO the
    same zone must not, or a 30px scroll off the floor re-sticks instantly."""
    res = _run("""
      scrollTo(4000);
      scrollTo(3970);            // 30px up — inside SCROLL_STICK_PX
      process.stdout.write(JSON.stringify({ stuck: _stickToBottom }));
    """)
    assert res["stuck"] is False


# ── our own scrolls are not the reader's ─────────────────────────────────────


def test_a_scroll_the_reader_makes_inside_one_frame_is_not_undone():
    """The regression the browser found. Scroll events are dispatched once per
    frame with the position as it is then, so a token arriving in the same
    frame as the wheel used to scroll back to the floor and leave a single
    event reporting the floor — the scroll reverted, with nothing ever saying
    it happened. maybeScrollToBottom reads the offset back before overwriting
    it, which is the one observation that cannot be raced."""
    res = _run("""
      grow(200); maybeScrollToBottom();      // a token: we own the floor
      msgs.scrollTop -= 150;                 // the reader's wheel, same frame
      grow(200); maybeScrollToBottom();      // the next token, before any event
      const top = msgs.scrollTop;
      onMessagesScroll();                    // the frame's one scroll event
      process.stdout.write(JSON.stringify({ top, stuck: _stickToBottom }));
    """)
    assert res["stuck"] is False
    assert res["top"] == 4050, "5200 - 1000 - 150: the reader stays where the wheel put them"


def test_a_self_scroll_that_moves_up_does_not_stop_the_stream():
    """_syncComposerHeightVar re-seats the transcript when the composer grows,
    and that write can lower scrollTop. Unrecorded it reads as the reader
    scrolling away, and the answer silently stops following itself."""
    res = _run("""
      scrollTo(4000);
      msgs.scrollTop = 3800;     // what the composer-height sync does
      noteSelfScroll(msgs);
      onMessagesScroll();        // the event that write provokes
      process.stdout.write(JSON.stringify({ stuck: _stickToBottom }));
    """)
    assert res["stuck"] is True


def test_resume_re_arms_following():
    res = _run("""
      scrollTo(4000);
      scrollTo(500);
      resumeFollowingStream();
      grow(300);
      maybeScrollToBottom();
      process.stdout.write(JSON.stringify({ top: msgs.scrollTop, floor: floor() }));
    """)
    assert res["top"] == res["floor"]


# ── the way back ─────────────────────────────────────────────────────────────


def test_the_jump_button_is_up_only_while_it_has_something_to_fix():
    res = _run("""
      scrollTo(4000);
      const atFloor = jump.hidden;
      scrollTo(1000);
      const away = jump.hidden;
      scrollTo(4000);
      const backAgain = jump.hidden;
      process.stdout.write(JSON.stringify({ atFloor, away, backAgain }));
    """)
    assert res["atFloor"] is True, "a reader who is already at the floor has nothing to jump to"
    assert res["away"] is False
    assert res["backAgain"] is True


# ── the wiring that makes the above reachable ────────────────────────────────


def test_the_old_distance_only_heuristic_is_gone():
    js = _read(CHAT_JS)
    assert "SCROLL_STICK_PX + 200" not in js, "the 320px slack zone is what ate every small scroll"


def test_every_scrolltop_write_on_the_transcript_is_recorded_as_ours():
    """A write this file performs that is not passed to noteSelfScroll can be
    read back as the reader moving — see the composer-height case above."""
    js = _read(CHAT_JS)
    assert js.count("noteSelfScroll(") >= 5, "the helper plus one call per write site"
    for site in (
        "el.scrollTop = el.scrollHeight;\n  noteSelfScroll(el);",
        "requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; noteSelfScroll(el); });",
        "msgs.scrollTop = msgs.scrollHeight - msgs.clientHeight - fromBottom;",
    ):
        assert site in js


def test_submitting_and_opening_a_transcript_both_re_arm_following():
    js = _read(CHAT_JS)
    assert "resumeFollowingStream();\n  renderMessage({ role: \"user\", content: text });" in js, (
        "a reader parked up the previous turn is put back on the stream by their own submit"
    )
    assert js.index("function scrollToLatestMessage() {") < js.index(
        "resumeFollowingStream();\n  el.scrollTop = el.scrollHeight;"
    )


def test_the_scroll_listener_is_registered_once_and_is_passive():
    js = _read(CHAT_JS)
    assert 'el.addEventListener("scroll", onMessagesScroll, { passive: true });' in js
    assert js.count("function onMessagesScroll(") == 1


def test_every_gesture_handler_is_passive():
    """They are the fast path, not the correctness path — a non-passive
    listener would tax the very gestures they exist to honour."""
    js = _read(CHAT_JS)
    wiring = js[js.index("(function wireStreamFollow()") : js.index('$("chat-input")?.focus();')]
    for evt in ("scroll", "wheel", "touchstart", "touchmove"):
        marker = f'el.addEventListener("{evt}"'
        assert marker in wiring
        assert "passive: true" in wiring.split(marker, 1)[1].split("addEventListener", 1)[0]


def test_only_an_upward_gesture_disarms_following():
    js = _read(CHAT_JS)
    assert "if (e.deltaY < 0 &&" in js, "a downward wheel is the reader coming back, not leaving"
    touchmove = js.split('el.addEventListener("touchmove"', 1)[1].split("}, { passive: true });", 1)[0]
    assert "y > _touchStartY + 4" in touchmove


def test_following_never_resumes_without_re_taking_the_baseline():
    """A stale baseline reads as drift, so a resume that skipped
    noteSelfScroll would unstick again on the very next token."""
    js = _read(CHAT_JS)
    body = js[js.index("function resumeFollowingStream(") : js.index("function onMessagesScroll(")]
    assert "noteSelfScroll(" in body


def test_the_jump_button_ships_hidden_and_cannot_leak_through_inline_flex():
    html = _read(CHAT_HTML)
    css = _read(CHAT_CSS)
    assert 'id="chat-jump-latest"' in html
    assert 'type="button"' in html.split('id="chat-jump-latest"')[0].rsplit("<button", 1)[1], (
        "inside <form id=chat-form> — a submit button here would send the composer"
    )
    assert ".cloud-chat-jump[hidden] { display: none; }" in css, (
        "display: inline-flex would otherwise beat the [hidden] default"
    )


# ── the gesture fast path only pre-disarms a gesture that can move the
#    transcript (Devin review on #2083) ───────────────────────────────────────


def _gesture(body: str) -> dict:
    return json.loads(_node_run(_gesture_predicate() + body))


def test_a_gesture_that_can_move_the_transcript_takes_the_fast_path():
    res = _gesture("""
      const el = node({ scrollTop: 400, scrollHeight: 5000, clientHeight: 1000 });
      const target = node({ _parent: el });
      process.stdout.write(JSON.stringify({ ok: gestureCanScrollTranscriptUp(el, target) }));
    """)
    assert res["ok"] is True


def test_a_transcript_with_nothing_to_give_does_not_disarm_following():
    """The stranding case: at the top, or not overflowing yet — the reader is
    still at the floor, so the recovery button is correctly hidden, and
    disarming here walked new tokens off the bottom of the screen with no way
    back."""
    res = _gesture("""
      const atTop = node({ scrollTop: 0, scrollHeight: 5000, clientHeight: 1000 });
      const short = node({ scrollTop: 0, scrollHeight: 300, clientHeight: 1000 });
      process.stdout.write(JSON.stringify({
        atTop: gestureCanScrollTranscriptUp(atTop, node({ _parent: atTop })),
        short: gestureCanScrollTranscriptUp(short, node({ _parent: short })),
        noElement: gestureCanScrollTranscriptUp(null, null),
      }));
    """)
    assert res == {"atTop": False, "short": False, "noElement": False}


def test_a_nested_scroller_that_eats_the_gesture_does_not_disarm_following():
    """A tool console or code block scrolled down consumes an upward wheel
    entirely. The event still bubbles to the transcript, which never moved."""
    res = _gesture("""
      const el = node({ scrollTop: 400, scrollHeight: 5000, clientHeight: 1000 });
      // A console scrolled down, with its own vertical overflow.
      const consoleScrolledDown = node({
        _parent: el, scrollTop: 90, scrollHeight: 600, clientHeight: 200, _overflowY: 'auto',
      });
      // The same console at ITS top: the gesture chains through to the transcript.
      const consoleAtItsTop = node({
        _parent: el, scrollTop: 0, scrollHeight: 600, clientHeight: 200, _overflowY: 'auto',
      });
      // A table wrapper is overflow-x only, so scrollTop never leaves 0.
      const tableWrap = node({
        _parent: el, scrollTop: 0, scrollHeight: 200, clientHeight: 200, _overflowY: 'visible',
      });
      process.stdout.write(JSON.stringify({
        eaten: gestureCanScrollTranscriptUp(el, node({ _parent: consoleScrolledDown })),
        chained: gestureCanScrollTranscriptUp(el, node({ _parent: consoleAtItsTop })),
        horizontalOnly: gestureCanScrollTranscriptUp(el, node({ _parent: tableWrap })),
      }));
    """)
    assert res["eaten"] is False
    assert res["chained"] is True, "a nested scroller at its own top does not eat the gesture"
    assert res["horizontalOnly"] is True


def test_both_gesture_handlers_are_gated_on_the_predicate():
    js = _read(CHAT_JS)
    assert "if (e.deltaY < 0 && gestureCanScrollTranscriptUp(el, e.target)) stopFollowingStream();" in js
    touchmove = js.split('el.addEventListener("touchmove"', 1)[1].split("}, { passive: true });", 1)[0]
    assert "gestureCanScrollTranscriptUp(el, e.target)" in touchmove

