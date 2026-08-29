"""Session-files drawer: instant tooltips and a durable save outcome (TCRD-212).

Follows tests/test_chat_facts_rendering_ui.py: the pure geometry is sliced
out of the SHIPPED chat.js and node-executed (no copies, no jsdom), while the
DOM-building and markup decisions are pinned structurally — the same
trade-off the pre-existing chat UI suite already makes.
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


def _tip_position_fn() -> str:
    js = _read(CHAT_JS)
    return js[js.index("function _tipPosition") : js.index("const Tip = (() => {")]


def _positions(cases: list) -> list:
    script = _tip_position_fn() + (
        f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}"
        ".map(([r, t, v]) => _tipPosition(r, t, v))));\n"
    )
    return json.loads(_node_run(script))


# ── geometry: really executed, not asserted about ──────────────────────────


def test_tooltip_sits_above_when_there_is_room():
    view = {"width": 1200, "height": 800}
    (pos,) = _positions([[{"top": 400, "bottom": 430, "left": 500, "width": 30}, {"width": 120, "height": 24}, view]])
    assert pos["below"] is False
    assert pos["top"] == 400 - 24 - 8


def test_tooltip_flips_below_when_the_trigger_is_near_the_top():
    """The drawer's own header buttons sit within ~50px of the viewport top,
    so this is the common case for Refresh/Close, not an edge case."""
    view = {"width": 1200, "height": 800}
    (pos,) = _positions([[{"top": 12, "bottom": 42, "left": 500, "width": 30}, {"width": 120, "height": 24}, view]])
    assert pos["below"] is True
    assert pos["top"] == 42 + 8


def test_tooltip_is_clamped_inside_the_right_edge():
    """The drawer docks to the trailing edge, so its action buttons are
    always near the viewport's right — an unclamped bubble would overflow."""
    view = {"width": 1200, "height": 800}
    (pos,) = _positions([[{"top": 400, "bottom": 430, "left": 1180, "width": 30}, {"width": 200, "height": 24}, view]])
    assert pos["left"] == 1200 - 200 - 8
    assert pos["left"] + 200 <= view["width"]


def test_tooltip_is_clamped_inside_the_left_edge():
    view = {"width": 1200, "height": 800}
    (pos,) = _positions([[{"top": 400, "bottom": 430, "left": 0, "width": 30}, {"width": 200, "height": 24}, view]])
    assert pos["left"] == 8


def test_tooltip_is_centred_on_the_trigger_when_unclamped():
    view = {"width": 1200, "height": 800}
    (pos,) = _positions([[{"top": 400, "bottom": 430, "left": 500, "width": 30}, {"width": 100, "height": 24}, view]])
    assert pos["left"] == 500 + 15 - 50


# ── the native-title regression this ticket exists to fix ──────────────────


def test_the_drawer_actions_no_longer_rely_on_native_title():
    """`title` has a ~1s delay, cannot be styled and never appears on
    keyboard focus — which is why nobody saw what these buttons did."""
    js = _read(CHAT_JS)
    row = js[js.index("function renderFileRow") : js.index("function updateFilesBadge")]
    assert ".title = " not in row, "a drawer action fell back to a native title tooltip"
    for tip in ("Download a copy", "Save to Library", "Open in your Library"):
        assert tip in row, f"missing data-tip copy: {tip}"


def test_drawer_header_buttons_carry_tips_and_keep_their_labels():
    html = _read(CHAT_HTML)
    head = html[html.index('id="chat-files-drawer"') : html.index('id="chat-files-status"')]
    assert 'data-tip="Refresh this list"' in head
    assert 'data-tip="Close"' in head
    assert 'title="Refresh"' not in head
    # The tooltip is an addition, never a replacement for the accessible name.
    assert 'aria-label="Refresh session files"' in head
    assert 'aria-label="Close session files"' in head


# ── a save has to leave a mark the reader can still see later ──────────────


def test_saving_records_the_outcome_in_the_row_not_only_in_a_toast():
    js = _read(CHAT_JS)
    row = js[js.index("function renderFileRow") : js.index("function updateFilesBadge")]
    assert "cloud-chat-files-saved" in row
    assert "Saved to Library" in row
    assert 'li.classList.add("is-saved")' in row
    # The toast stays — it is the immediate acknowledgement — but it is no
    # longer the only record.
    assert 'showToast("Saved to your Library"' in row


def test_tooltip_is_shown_on_focus_not_only_hover():
    js = _read(CHAT_JS)
    tip = js[js.index("const Tip = (() => {") : js.index("/** Set the title strip")]
    assert '"focusin"' in tip, "keyboard users must get the same affordance as mouse users"
    assert "aria-describedby" in tip


def test_tooltip_retires_rather_than_following_a_moved_anchor():
    """The drawer's list scrolls; a bubble left behind at a stale rect is
    worse than no bubble."""
    js = _read(CHAT_JS)
    tip = js[js.index("const Tip = (() => {") : js.index("/** Set the title strip")]
    assert '"scroll"' in tip
    assert '"resize"' in tip
    assert "Escape" in tip


# ── contrast: the DES-134 failure must not be repeated ─────────────────────


def test_tooltip_colour_is_an_inversion_of_the_theme_pair():
    """DES-134 found tooltips rendering white-on-white. Deriving the bubble
    from the page's own text/surface pair makes contrast structural rather
    than a value someone picked for one theme."""
    css = _read(CHAT_CSS)
    block = css[css.index(".ds-tip {") : css.index(".ds-tip[hidden]")]
    assert "background: var(--ds-text-primary)" in block
    assert "color: var(--ds-surface)" in block
    assert "#" not in block, "a raw hex would reintroduce a per-theme guess"
